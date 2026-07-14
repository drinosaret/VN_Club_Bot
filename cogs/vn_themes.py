# cogs/vn_themes.py
"""/manage_theme: manager dashboard for themed-nomination gating.

Templates are a per-guild reusable library; applying one snapshots its rules
onto a period (guild, kind, start_month, end_month) that /nominate reads.
Everything here is manager-gated (validate_user_permission) and guild-scoped.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from lib.bot import VNClubBot
from lib.utils import (
    DatabaseQueries,
    ValidationError,
    validate_user_permission,
    handle_command_error,
    validate_month_format,
    get_current_month,
    next_month,
    current_anime_season,
    next_season,
    season_to_months,
    month_to_season_name,
    format_season_label,
)
from lib.themes import validate_rules, rules_summary, RULES_SCHEMA_VERSION
from lib.vndb_search import VNDBClient

_log = logging.getLogger(__name__)


def _resolve_period(kind: str, target_month: Optional[str]):
    """Mirror /nominate's window logic: returns (start_month, end_month).
    Raises ValidationError on a malformed target_month."""
    if kind == "seasonal":
        if target_month:
            if not validate_month_format(target_month):
                raise ValidationError("bad month", "target_month must be YYYY-MM.")
            season_name = month_to_season_name(int(target_month[5:7]))
            year_int = int(target_month[:4])
        else:
            cur_season, cur_year = current_anime_season()
            year_int, season_name = next_season(cur_year, cur_season)
        months = season_to_months(season_name, year_int)
        return months[0], months[-1]
    target = target_month or next_month(get_current_month())
    if not validate_month_format(target):
        raise ValidationError("bad month", "target_month must be YYYY-MM.")
    return target, target


class ThemeCog(commands.Cog):
    def __init__(self, bot: VNClubBot):
        self.bot = bot

    @app_commands.command(name="manage_theme", description="[MANAGER] Manage nomination themes for this server.")
    @app_commands.guild_only()
    async def manage_theme(self, interaction: discord.Interaction):
        try:
            await validate_user_permission(interaction)
        except ValidationError as e:
            await handle_command_error(interaction, e)
            return
        state = await _fetch_theme_state(self.bot, interaction.guild.id)
        view = ThemePanelView(self, state)
        await interaction.response.send_message(
            content=_build_panel_text(state), view=view, ephemeral=True,
        )
        # Keep a handle on the panel message so sub-flows (save/delete/assign),
        # whose own interaction targets a different message, can refresh it.
        view.message = await interaction.original_response()


async def _fetch_theme_state(bot: VNClubBot, guild_id: int) -> dict:
    templates = await bot.GET(DatabaseQueries.LIST_THEME_TEMPLATES, (guild_id,))
    assignments = await bot.GET(DatabaseQueries.LIST_THEME_ASSIGNMENTS, (guild_id,))
    return {"guild_id": guild_id, "templates": templates, "assignments": assignments}


def _build_panel_text(state: dict) -> str:
    lines = ["## Nomination themes", ""]
    tpls = state["templates"]
    lines.append(f"**Templates ({len(tpls)}):** " + (
        ", ".join(t[1] for t in tpls) if tpls else "none yet"))
    lines.append("")
    if state["assignments"]:
        lines.append("**Active / upcoming themed periods:**")
        for kind, start, end, label, _rules in state["assignments"]:
            window = start if start == end else f"{start} to {end}"
            lines.append(f"• `{kind}` {window}: **{label}**")
    else:
        lines.append("**No themed periods set.** Nominations are unrestricted.")
    return "\n".join(lines)


class ThemePanelView(discord.ui.View):
    """Ephemeral dashboard. Not persisted; user re-runs /manage_theme to
    refresh after the 600s timeout."""

    def __init__(self, cog: ThemeCog, state: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.state = state
        self.message = None  # set after send in manage_theme; used by refresh
        self._build_items()

    def _build_items(self) -> None:
        # Rebuilt on every refresh so the Templates button appears once the
        # first template exists (and disappears if all are deleted).
        self.clear_items()
        self.add_item(_NewTemplateButton(self))
        if self.state["templates"]:
            self.add_item(_TemplatesButton(self))
        self.add_item(_AssignButton(self))
        self.add_item(_ClearPeriodButton(self))

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.state = await _fetch_theme_state(self.cog.bot, self.state["guild_id"])
        self._build_items()
        content = _build_panel_text(self.state)
        # Update the panel via its own stored message: sub-flows call refresh
        # from an interaction whose response targets a DIFFERENT message
        # (the editor or an ephemeral picker), so editing the interaction's
        # original response would rewrite the wrong message.
        edited_panel = False
        if self.message is not None:
            try:
                await self.message.edit(content=content, view=self)
                edited_panel = True
            except discord.HTTPException:
                pass
        # Ensure the triggering interaction is acknowledged. If we could not
        # reach the panel message, fall back to editing through the interaction.
        if not interaction.response.is_done():
            if edited_panel:
                await interaction.response.defer()
            else:
                await interaction.response.edit_message(content=content, view=self)
        elif not edited_panel:
            await interaction.edit_original_response(content=content, view=self)


class _ClearPeriodButton(discord.ui.Button):
    def __init__(self, panel: ThemePanelView):
        super().__init__(style=discord.ButtonStyle.danger, label="Clear a period's theme", emoji="🧹")
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ClearPeriodModal(self.panel))


class _ClearPeriodModal(discord.ui.Modal, title="Clear a period's theme"):
    def __init__(self, panel: ThemePanelView):
        super().__init__(timeout=300)
        self.panel = panel
        self.kind_input = discord.ui.TextInput(label="Kind (monthly or seasonal)", default="monthly", max_length=10)
        self.month_input = discord.ui.TextInput(
            label="Target month (YYYY-MM, blank = next)", required=False, max_length=7)
        self.add_item(self.kind_input)
        self.add_item(self.month_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            kind = (self.kind_input.value or "monthly").strip().lower()
            if kind not in ("monthly", "seasonal"):
                raise ValidationError("bad kind", "Kind must be monthly or seasonal.")
            start, end = _resolve_period(kind, (self.month_input.value or "").strip() or None)
            await self.panel.cog.bot.RUN(
                DatabaseQueries.DELETE_THEME_ASSIGNMENT,
                (interaction.guild.id, kind, start, end),
            )
            await self.panel.refresh(interaction)
        except ValidationError as e:
            await interaction.response.send_message(f"❌ {e.user_message}", ephemeral=True)


# ---------------- template editor (Task 9) ----------------


def _str_or_blank(v) -> str:
    return "" if v is None else str(v)


def _int_range_from_inputs(min_raw: str, max_raw: str) -> Optional[dict]:
    def parse(s):
        s = (s or "").strip()
        return int(s) if s else None
    lo, hi = parse(min_raw), parse(max_raw)
    if lo is None and hi is None:
        return None
    return {"min": lo, "max": hi}


def _set_or_clear(rules: dict, key: str, node) -> None:
    if node is None:
        rules.pop(key, None)
    else:
        rules[key] = node


def _nsfw_label(value) -> str:
    return f"NSFW cover: {value if value else 'any'}"


class ThemeEditorView(discord.ui.View):
    """Builds one template's rules facet-by-facet. On Save, validate_rules
    then INSERT (new) or UPDATE (existing template_id)."""

    def __init__(self, cog: ThemeCog, panel: ThemePanelView,
                 template_id: Optional[int], name: str, rules: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.panel = panel
        self.template_id = template_id
        self.name = name
        self.rules = rules  # unvalidated working dict
        # Set by the opener button once the editor message is sent, so rerender
        # can edit the editor from an ephemeral search followup interaction.
        self.message: Optional[discord.Message] = None
        self.add_item(_EditNameButton(self))
        self.add_item(_EditLengthButton(self))
        self.add_item(_EditCharsButton(self))
        self.add_item(_EditReleaseButton(self))
        self.add_item(_ToggleNsfwButton(self))
        self.add_item(_AddTagButton(self))
        self.add_item(_AddDeveloperButton(self))
        self.add_item(_SaveTemplateButton(self))

    def content(self) -> str:
        try:
            preview = rules_summary(validate_rules(self.rules))
        except ValueError as e:
            preview = f"(invalid: {e})"
        return f"### Editing template: **{self.name or '(unnamed)'}**\n{preview}"

    async def rerender(self, interaction: discord.Interaction) -> None:
        content = self.content()
        edited = False
        # Facet modals + editor buttons: the interaction message IS the editor.
        if not interaction.response.is_done():
            try:
                await interaction.response.edit_message(content=content, view=self)
                edited = True
            except discord.HTTPException:
                pass
        # Tag/developer picks come from a separate ephemeral followup, so the
        # interaction can't reach the editor message; edit the stored ref
        # instead. Wrapped so a chip add never hard-fails. Live-verified.
        if not edited and self.message is not None:
            try:
                await self.message.edit(content=content, view=self)
            except discord.HTTPException:
                pass


class _EditNameButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Name", emoji="🏷️")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_NameModal(self.ed))


class _NameModal(discord.ui.Modal, title="Template name"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        self.field = discord.ui.TextInput(label="Name", default=ed.name, max_length=80)
        self.add_item(self.field)

    async def on_submit(self, interaction):
        self.ed.name = (self.field.value or "").strip()
        await self.ed.rerender(interaction)


class _EditLengthButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Length", emoji="📏")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_LengthModal(self.ed))


class _LengthModal(discord.ui.Modal, title="Length rating (1-5, blank = unset)"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        cur = ed.rules.get("length_rating") or {}
        self.min_f = discord.ui.TextInput(label="Min (1=Very short .. 5=Very long)", required=False,
                                          default=_str_or_blank(cur.get("min")), max_length=1)
        self.max_f = discord.ui.TextInput(label="Max", required=False,
                                          default=_str_or_blank(cur.get("max")), max_length=1)
        self.add_item(self.min_f)
        self.add_item(self.max_f)

    async def on_submit(self, interaction):
        node = _int_range_from_inputs(self.min_f.value, self.max_f.value)
        _set_or_clear(self.ed.rules, "length_rating", node)
        await self.ed.rerender(interaction)


class _EditCharsButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Characters", emoji="🔤")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_CharsModal(self.ed))


class _CharsModal(discord.ui.Modal, title="Character count (blank = unset)"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        cur = ed.rules.get("character_count") or {}
        self.min_f = discord.ui.TextInput(label="Min characters", required=False,
                                          default=_str_or_blank(cur.get("min")), max_length=9)
        self.max_f = discord.ui.TextInput(label="Max characters", required=False,
                                          default=_str_or_blank(cur.get("max")), max_length=9)
        self.add_item(self.min_f)
        self.add_item(self.max_f)

    async def on_submit(self, interaction):
        node = _int_range_from_inputs(self.min_f.value, self.max_f.value)
        _set_or_clear(self.ed.rules, "character_count", node)
        await self.ed.rerender(interaction)


class _EditReleaseButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Release", emoji="📆")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_ReleaseModal(self.ed))


class _ReleaseModal(discord.ui.Modal, title="Release window"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        cur = ed.rules.get("released") or {}
        self.after_f = discord.ui.TextInput(label="After (YYYY-MM-DD, blank = any)", required=False,
                                            default=_str_or_blank(cur.get("after")), max_length=10)
        self.before_f = discord.ui.TextInput(label="Before", required=False,
                                             default=_str_or_blank(cur.get("before")), max_length=10)
        self.add_item(self.after_f)
        self.add_item(self.before_f)

    async def on_submit(self, interaction):
        after = (self.after_f.value or "").strip()
        before = (self.before_f.value or "").strip()
        node = {"after": after or None, "before": before or None}
        _set_or_clear(self.ed.rules, "released", node if after or before else None)
        await self.ed.rerender(interaction)


class _ToggleNsfwButton(discord.ui.Button):
    """Cycle nsfw_cover none -> disallow -> allow -> none."""

    _NEXT = {None: "disallow", "disallow": "allow", "allow": None}

    def __init__(self, ed):
        super().__init__(label=_nsfw_label(ed.rules.get("nsfw_cover")))
        self.ed = ed

    async def callback(self, interaction):
        nxt = self._NEXT.get(self.ed.rules.get("nsfw_cover"), "disallow")
        _set_or_clear(self.ed.rules, "nsfw_cover", nxt)
        self.label = _nsfw_label(nxt)
        await self.ed.rerender(interaction)


class _AddTagButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Add tag", emoji="🏷️")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_TagSearchModal(self.ed))


class _TagSearchModal(discord.ui.Modal, title="Search VNDB tags"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        self.q = discord.ui.TextInput(label="Tag name", max_length=60)
        self.add_item(self.q)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        async with VNDBClient() as c:
            results = await c.search_tags(self.q.value)
        if not results:
            await interaction.followup.send("No tags matched.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_TagResultSelect(self.ed, results[:25]))
        await interaction.followup.send("Pick a tag:", view=view, ephemeral=True)


class _TagResultSelect(discord.ui.Select):
    def __init__(self, ed, results):
        self.ed = ed
        self._by_id = {r["id"]: r for r in results}
        options = [discord.SelectOption(label=r["name"][:100], value=r["id"],
                                        description=(r.get("category") or None)) for r in results]
        super().__init__(placeholder="Tag", options=options)

    async def callback(self, interaction):
        chosen = self._by_id[self.values[0]]
        view = discord.ui.View(timeout=300)
        view.add_item(_TagBucketSelect(self.ed, chosen))
        await interaction.response.edit_message(
            content=f"Add **{chosen['name']}** as:", view=view)


class _TagBucketSelect(discord.ui.Select):
    def __init__(self, ed, chosen):
        self.ed = ed
        self.chosen = chosen
        super().__init__(placeholder="Rule", options=[
            discord.SelectOption(label="Must have (all of)", value="all_of"),
            discord.SelectOption(label="Any of", value="any_of"),
            discord.SelectOption(label="Exclude (none of)", value="none_of"),
        ])

    async def callback(self, interaction):
        bucket = self.values[0]
        tags = self.ed.rules.setdefault("tags", {"all_of": [], "any_of": [], "none_of": []})
        entry = {"id": self.chosen["id"], "name": self.chosen["name"]}
        lst = tags.setdefault(bucket, [])
        if not any(e["id"] == entry["id"] for e in lst):
            lst.append(entry)
        await interaction.response.edit_message(
            content=f"Added **{entry['name']}** to {bucket}.", view=None)
        await self.ed.rerender(interaction)  # updates the editor message too


class _AddDeveloperButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(label="Add developer", emoji="🏢")
        self.ed = ed

    async def callback(self, interaction):
        await interaction.response.send_modal(_DeveloperSearchModal(self.ed))


class _DeveloperSearchModal(discord.ui.Modal, title="Search VNDB developers"):
    def __init__(self, ed):
        super().__init__(timeout=300)
        self.ed = ed
        self.q = discord.ui.TextInput(label="Developer name", max_length=60)
        self.add_item(self.q)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        async with VNDBClient() as c:
            results = await c.search_producers(self.q.value)
        if not results:
            await interaction.followup.send("No developers matched.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_DeveloperResultSelect(self.ed, results[:25]))
        await interaction.followup.send("Pick a developer:", view=view, ephemeral=True)


class _DeveloperResultSelect(discord.ui.Select):
    def __init__(self, ed, results):
        self.ed = ed
        self._by_id = {r["id"]: r for r in results}
        options = [discord.SelectOption(label=r["name"][:100], value=r["id"]) for r in results]
        super().__init__(placeholder="Developer", options=options)

    async def callback(self, interaction):
        chosen = self._by_id[self.values[0]]
        devs = self.ed.rules.setdefault("developers", {"any_of": []})
        lst = devs.setdefault("any_of", [])
        entry = {"id": chosen["id"], "name": chosen["name"]}
        if not any(e["id"] == entry["id"] for e in lst):
            lst.append(entry)
        await interaction.response.edit_message(
            content=f"Added developer **{entry['name']}**.", view=None)
        await self.ed.rerender(interaction)  # updates the editor message too


class _SaveTemplateButton(discord.ui.Button):
    def __init__(self, ed):
        super().__init__(style=discord.ButtonStyle.success, label="Save template", emoji="💾")
        self.ed = ed

    async def callback(self, interaction):
        if not self.ed.name:
            await interaction.response.send_message("Give the template a name first.", ephemeral=True)
            return
        try:
            canonical = validate_rules(self.ed.rules)
        except ValueError as e:
            await interaction.response.send_message(f"❌ Invalid rules: {e}", ephemeral=True)
            return
        rules_json = json.dumps(canonical)
        try:
            if self.ed.template_id is None:
                await self.ed.cog.bot.RUN_RETURNING_ID(
                    DatabaseQueries.INSERT_THEME_TEMPLATE,
                    (interaction.guild.id, self.ed.name, rules_json, interaction.user.id))
            else:
                await self.ed.cog.bot.RUN(
                    DatabaseQueries.UPDATE_THEME_TEMPLATE,
                    (self.ed.name, rules_json, self.ed.template_id, interaction.guild.id))
        except aiosqlite.IntegrityError:
            # The only expected failure: UNIQUE(guild_id, name) collision.
            await interaction.response.send_message(
                f"❌ A template named **{self.ed.name}** already exists.", ephemeral=True)
            return
        except Exception:
            # A real failure (locked db, query bug, ...) must not masquerade as a
            # name collision, and must leave a log line to diagnose.
            _log.exception(
                "theme template save failed (guild=%s name=%r)",
                interaction.guild.id, self.ed.name)
            await interaction.response.send_message(
                "❌ Couldn't save the template right now (an internal error occurred). Try again.",
                ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"✅ Saved template **{self.ed.name}**.", view=None)
        await self.ed.panel.refresh(interaction)


class _NewTemplateButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(style=discord.ButtonStyle.primary, label="New template", emoji="➕")
        self.panel = panel

    async def callback(self, interaction):
        ed = ThemeEditorView(self.panel.cog, self.panel, None, "", {"schema_version": RULES_SCHEMA_VERSION})
        await interaction.response.send_message(content=ed.content(), view=ed, ephemeral=True)
        ed.message = await interaction.original_response()


class _TemplatesButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(label="Templates", emoji="📚")
        self.panel = panel

    async def callback(self, interaction):
        view = discord.ui.View(timeout=300)
        view.add_item(_TemplatePickSelect(self.panel))
        await interaction.response.send_message("Pick a template:", view=view, ephemeral=True)


class _TemplatePickSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        opts = [discord.SelectOption(label=t[1][:100], value=str(t[0]))
                for t in panel.state["templates"][:25]]
        super().__init__(placeholder="Template", options=opts)

    async def callback(self, interaction):
        tid = int(self.values[0])
        row = await self.panel.cog.bot.GET_ONE(
            DatabaseQueries.GET_THEME_TEMPLATE, (tid, interaction.guild.id))
        if not row:
            await interaction.response.edit_message(content="Template not found.", view=None)
            return
        _id, name, rules_json = row
        view = discord.ui.View(timeout=300)
        view.add_item(_EditTemplateButton(self.panel, tid, name, rules_json))
        view.add_item(_DuplicateTemplateButton(self.panel, name, rules_json))
        view.add_item(_DeleteTemplateButton(self.panel, tid, name))
        await interaction.response.edit_message(content=f"**{name}**", view=view)


class _EditTemplateButton(discord.ui.Button):
    def __init__(self, panel, template_id, name, rules_json):
        super().__init__(label="Edit", emoji="✏️")
        self.panel = panel
        self.template_id = template_id
        self.name = name
        self.rules_json = rules_json

    async def callback(self, interaction):
        ed = ThemeEditorView(self.panel.cog, self.panel, self.template_id,
                             self.name, json.loads(self.rules_json))
        await interaction.response.send_message(content=ed.content(), view=ed, ephemeral=True)
        ed.message = await interaction.original_response()


class _DuplicateTemplateButton(discord.ui.Button):
    def __init__(self, panel, name, rules_json):
        super().__init__(label="Duplicate", emoji="📄")
        self.panel = panel
        self.name = name
        self.rules_json = rules_json

    async def callback(self, interaction):
        # template_id=None so Save writes a NEW row; the original is untouched.
        ed = ThemeEditorView(self.panel.cog, self.panel, None,
                             f"{self.name} copy", json.loads(self.rules_json))
        await interaction.response.send_message(content=ed.content(), view=ed, ephemeral=True)
        ed.message = await interaction.original_response()


class _DeleteTemplateButton(discord.ui.Button):
    def __init__(self, panel, template_id, name):
        super().__init__(style=discord.ButtonStyle.danger, label="Delete", emoji="🗑️")
        self.panel = panel
        self.template_id = template_id
        self.name = name

    async def callback(self, interaction):
        # Deleting a template does not touch assignments: they hold a snapshot.
        await self.panel.cog.bot.RUN(
            DatabaseQueries.DELETE_THEME_TEMPLATE, (self.template_id, interaction.guild.id))
        await interaction.response.edit_message(content=f"Deleted **{self.name}**.", view=None)
        await self.panel.refresh(interaction)


# ---------------- assign a template to a period (Task 10) ----------------


class _AssignButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(style=discord.ButtonStyle.primary, label="Assign to period", emoji="📅")
        self.panel = panel

    async def callback(self, interaction):
        if not self.panel.state["templates"]:
            await interaction.response.send_message("Create a template first.", ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        view.add_item(_AssignTemplateSelect(self.panel))
        await interaction.response.send_message("Template to apply:", view=view, ephemeral=True)


class _AssignTemplateSelect(discord.ui.Select):
    def __init__(self, panel):
        self.panel = panel
        opts = [discord.SelectOption(label=t[1][:100], value=str(t[0]))
                for t in panel.state["templates"][:25]]
        super().__init__(placeholder="Template", options=opts)

    async def callback(self, interaction):
        await interaction.response.send_modal(_AssignPeriodModal(self.panel, int(self.values[0])))


class _AssignPeriodModal(discord.ui.Modal, title="Apply theme to a period"):
    def __init__(self, panel, template_id):
        super().__init__(timeout=300)
        self.panel = panel
        self.template_id = template_id
        self.kind_f = discord.ui.TextInput(label="Kind (monthly or seasonal)", default="monthly", max_length=10)
        self.month_f = discord.ui.TextInput(label="Target month (YYYY-MM, blank = next)", required=False, max_length=7)
        self.label_f = discord.ui.TextInput(label="Display label (blank = template name)", required=False, max_length=80)
        self.add_item(self.kind_f)
        self.add_item(self.month_f)
        self.add_item(self.label_f)

    async def on_submit(self, interaction):
        try:
            kind = (self.kind_f.value or "monthly").strip().lower()
            if kind not in ("monthly", "seasonal"):
                raise ValidationError("bad kind", "Kind must be monthly or seasonal.")
            start, end = _resolve_period(kind, (self.month_f.value or "").strip() or None)
            row = await self.panel.cog.bot.GET_ONE(
                DatabaseQueries.GET_THEME_TEMPLATE, (self.template_id, interaction.guild.id))
            if not row:
                raise ValidationError("missing", "That template no longer exists.")
            _id, tpl_name, rules_json = row
            label = (self.label_f.value or "").strip() or tpl_name
            await self.panel.cog.bot.RUN(
                DatabaseQueries.UPSERT_THEME_ASSIGNMENT,
                (interaction.guild.id, kind, start, end, label, rules_json,
                 self.template_id, interaction.user.id))
            await self.panel.refresh(interaction)
        except ValidationError as e:
            await interaction.response.send_message(f"❌ {e.user_message}", ephemeral=True)


async def setup(bot: VNClubBot):
    await bot.add_cog(ThemeCog(bot))
